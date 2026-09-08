"""Behavior tests for device validation, import, and cached retrieval."""

from copy import deepcopy

import pytest
from django.core.cache import cache
from django.db import connection

from netbox_librenms_plugin.tests.conftest import (
    ip_on,
    make_cluster,
    make_device,
    make_ip,
    make_virtual_chassis,
    make_vm,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


SERVER_KEY = "test-server"


@pytest.mark.django_db
def test_trimmed_serial_lookup_uses_expression_index():
    from netbox_librenms_plugin.utils import find_devices_by_serial

    device = make_device("padded-serial", serial=" \t\n\r\v\fSERIAL-TRIM \t\n\r\v\f")
    queries = []

    def capture_query(execute, sql, params, many, context):
        queries.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture_query):
        assert find_devices_by_serial("SERIAL-TRIM") == [device]

    fallback_queries = [(sql, params) for sql, params in queries if "BTRIM(" in sql]
    assert len(fallback_queries) == 1
    sql, params = fallback_queries[0]
    index_name = "nblp_dcim_device_serial_trim_idx"
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
        assert index_name in plan, plan

        cursor.execute(
            """
            SELECT indisvalid, indrelid = to_regclass('dcim_device'),
                   pg_get_expr(indexprs, indrelid)
            FROM pg_index
            WHERE indexrelid = to_regclass(%s)
            """,
            [index_name],
        )
        index = cursor.fetchone()
    assert index is not None
    valid, on_device, expression = index
    assert valid
    assert on_device
    assert "btrim" in expression.lower()
    assert "serial" in expression


def _device_payload(device_id=4101, **overrides):
    """Return one complete LibreNMS device response row."""
    payload = {
        "device_id": device_id,
        "hostname": f"edge-{device_id}.example.test",
        "sysName": f"edge-{device_id}",
        "hardware": "Test Router",
        "serial": f"SERIAL-{device_id}",
        "os": "linux",
        "ip": f"198.18.0.{device_id % 250 + 1}",
        "version": "1.0",
        "location": "Test Lab",
        "type": "network",
        "status": 1,
    }
    payload.update(overrides)
    return payload


def _configure_server(settings, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "Test LibreNMS",
            "librenms_url": server.url,
            "api_token": "test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture(autouse=True)
def isolated_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def librenms_api(settings):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    with librenms_mock_server() as server:
        _configure_server(settings, server)
        yield LibreNMSAPI(server_key=SERVER_KEY), server


def _stack_root(index=100):
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "stack",
        "entPhysicalSerialNum": "",
        "entPhysicalModelName": "",
        "entPhysicalName": "StackSub-0/0",
        "entPhysicalDescr": "Test stack",
        "entPhysicalContainedIn": 0,
    }


def _chassis(index, serial, position, model="Test Router"):
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "chassis",
        "entPhysicalSerialNum": serial,
        "entPhysicalModelName": model,
        "entPhysicalName": f"Chassis-{position}",
        "entPhysicalDescr": f"Member {position}",
        "entPhysicalParentRelPos": position,
    }


def _validation(*, site=None, device_type=None, role=None, resolved_name="imported-edge", **overrides):
    """Build the public validation contract needed by import_single_device."""
    result = {
        "existing_device": None,
        "existing_match_type": None,
        "ambiguous_librenms_id": False,
        "resolved_name": resolved_name,
        "site": {"site": site},
        "device_type": {"device_type": device_type},
        "device_role": {"role": role},
        "platform": {"platform": None},
        "rack": {"rack": None},
    }
    result.update(overrides)
    return result


class TestDetermineDeviceName:
    @pytest.mark.parametrize(
        ("payload", "use_sysname", "strip_domain", "device_id", "expected"),
        [
            ({"sysName": "system", "hostname": "host"}, True, False, 1, "system"),
            ({"sysName": "system", "hostname": "host"}, False, False, 1, "host"),
            ({"sysName": "", "hostname": "host"}, True, False, 1, "host"),
            ({"sysName": None, "hostname": None}, True, False, 7, "device-7"),
            ({"device_id": 8}, True, False, None, "device-8"),
            ({"sysName": "edge.example.test"}, True, True, 1, "edge"),
            ({"sysName": "edge.a.example.test"}, True, True, 1, "edge"),
            ({"sysName": "198.18.0.7"}, True, True, 1, "198.18.0.7"),
            ({"sysName": ".example.test"}, True, True, 9, "device-9"),
            ({"sysName": 123, "hostname": ["bad"]}, True, False, 10, "device-10"),
            ({"sysName": 7, "hostname": "host"}, True, False, 1, "host"),
        ],
    )
    def test_name_resolution(self, payload, use_sysname, strip_domain, device_id, expected):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        assert (
            _determine_device_name(
                payload,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                device_id=device_id,
            )
            == expected
        )

    def test_resolve_device_name_reports_the_value_the_name_came_from(self):
        """The source follows the post-strip decision: a stripped-empty sysName is the id fallback."""
        from netbox_librenms_plugin.import_utils.device_operations import _resolve_device_name

        assert _resolve_device_name({"sysName": "", "hostname": "sw01.example.test"}, strip_domain=True) == (
            "sw01",
            "hostname",
        )
        assert _resolve_device_name(
            {"sysName": ".example.test", "hostname": "sw02"}, strip_domain=True, device_id=7
        ) == (
            "device-7",
            "device-7",
        )


class TestOobNormalization:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("leaf01-oob-idrac9", "idrac"),
            ("leaf01-oob", "oob"),
            ("server-ilo5", "ilo"),
            ("Cisco-CIMC", "cimc"),
            ("normal-router", None),
            ("", None),
        ],
    )
    def test_name_detection_prefers_specific_controller_types(self, name, expected):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_oob_type_from_name

        assert _detect_oob_type_from_name(name) == expected

    @pytest.mark.parametrize(
        ("os_name", "hardware", "expected"),
        [
            ("oob", "iDRAC9", "idrac"),
            ("drac9", "iDRAC9", "drac"),
            ("", "Cisco CIMC", "cimc"),
            ("dracut", "ipmitool", None),
            ("oob", "", "oob"),
        ],
    )
    def test_payload_normalization_uses_whole_tokens(self, os_name, hardware, expected):
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type(os_name, hardware) == expected


class TestLinkNotes:
    @pytest.mark.parametrize(
        ("link", "expected"),
        [
            ({"host_id": 42, "oob_id": None}, "currently linked to LibreNMS device #42"),
            ({"host_id": None, "oob_id": 7}, "currently linked to LibreNMS as an OOB controller"),
            ({"host_id": 42, "oob_id": 7}, "currently linked to LibreNMS device #42"),
            ({"host_id": None, "oob_id": None}, "not linked to LibreNMS"),
            (None, "not linked to LibreNMS"),
        ],
    )
    def test_link_note_contract(self, link, expected):
        from netbox_librenms_plugin.import_utils.device_operations import _describe_link_note

        assert _describe_link_note(link) == expected

    @pytest.mark.django_db
    def test_existing_link_description_reads_real_custom_field_data(self):
        from netbox_librenms_plugin.import_utils.device_operations import _describe_existing_librenms_link

        device = make_device(
            "described-link",
            librenms_cf={SERVER_KEY: {"id": "42", "oob": {"id": "7", "type": "idrac"}}},
        )

        assert _describe_existing_librenms_link(device, SERVER_KEY) == {
            "host_id": 42,
            "oob_id": 7,
            "oob_type": "idrac",
        }


@pytest.mark.django_db
class TestChassisDeviceTypeMatch:
    def _device_type(self, tag, *, model, part_number=""):
        from dcim.models import DeviceType, Manufacturer

        manufacturer, _ = Manufacturer.objects.get_or_create(
            name=f"Chassis manufacturer {tag}",
            slug=f"chassis-manufacturer-{tag}",
        )
        return DeviceType.objects.create(
            manufacturer=manufacturer,
            model=model,
            slug=f"chassis-type-{tag}",
            part_number=part_number,
        )

    def test_real_inventory_name_matches_a_real_device_type(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api, server = librenms_api
        matched = self._device_type("name", model="MX480", part_number="CHAS-BP-MX480-S")
        server.register(
            "/api/v0/inventory/5101",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalName": "CHAS-BP-MX480-S",
                        "entPhysicalModelName": "710-017414",
                    }
                ],
            },
        )

        result = _try_chassis_device_type_match(api, 5101)

        assert result["matched"] is True
        assert result["device_type"] == matched
        assert result["match_type"] == "chassis"
        assert result["chassis_model"] == "CHAS-BP-MX480-S"

    def test_inventory_model_is_used_after_an_unmatched_name(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api, server = librenms_api
        matched = self._device_type("model", model="Model 710-017414")
        server.register(
            "/api/v0/inventory/5102",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalName": "Unspecified",
                        "entPhysicalModelName": "Model 710-017414",
                    }
                ],
            },
        )

        result = _try_chassis_device_type_match(api, 5102)

        assert result["device_type"] == matched
        assert result["chassis_model"] == "Model 710-017414"

    @pytest.mark.parametrize("status", [404, 500])
    def test_inventory_lookup_failure_returns_no_match(self, librenms_api, status):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api, server = librenms_api
        server.register("/api/v0/inventory/5103", {"status": "error"}, status=status)

        assert _try_chassis_device_type_match(api, 5103) is None

    def test_empty_and_placeholder_inventory_have_no_match(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api, server = librenms_api
        server.register(
            "/api/v0/inventory/5104",
            {
                "status": "ok",
                "inventory": [{"entPhysicalName": "-", "entPhysicalModelName": "BUILTIN"}],
            },
        )

        assert _try_chassis_device_type_match(api, 5104) is None

    def test_ambiguous_inventory_model_has_no_match(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api, server = librenms_api
        self._device_type("ambiguous-a", model="Shared chassis model")
        self._device_type("ambiguous-b", model="Shared chassis model")
        server.register(
            "/api/v0/inventory/5105",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalName": "Unspecified",
                        "entPhysicalModelName": "Shared chassis model",
                    }
                ],
            },
        )

        assert _try_chassis_device_type_match(api, 5105) is None


@pytest.mark.django_db
class TestDeviceFetching:
    def test_live_device_lookup_uses_the_real_http_client(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api, server = librenms_api
        server.device_info_response(device_id=5201, hostname="live-device")

        result = get_librenms_device_by_id(api, 5201, use_cache=False)

        assert result["device_id"] == 5201
        assert result["hostname"] == "live-device"

    @pytest.mark.parametrize("status", [404, 500])
    def test_absent_or_failed_live_lookup_returns_none(self, librenms_api, status):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api, server = librenms_api
        server.register(
            "/api/v0/devices/5202",
            {"status": "error"},
            status=status,
        )

        assert get_librenms_device_by_id(api, 5202, use_cache=False) is None

    def test_prefetched_cache_is_first_and_rejects_a_mis_keyed_row(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api, server = librenms_api
        prefetched = {5203: _device_payload(5203, hostname="prefetched")}
        assert fetch_device_with_cache(5203, api, libre_devices_cache=prefetched)["hostname"] == "prefetched"

        server.device_info_response(device_id=5204, hostname="live-after-miskey")
        mis_keyed = {5204: _device_payload(9999, hostname="wrong")}
        assert fetch_device_with_cache(5204, api, libre_devices_cache=mis_keyed)["hostname"] == "live-after-miskey"

    def test_django_cache_precedes_http_and_is_server_scoped(self, librenms_api):
        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api, _server = librenms_api
        cached = _device_payload(5205, hostname="django-cache")
        cache.set(get_import_device_cache_key(5205, SERVER_KEY), cached, 300)

        assert fetch_device_with_cache(5205, api, server_key=SERVER_KEY) == cached
        assert cache.get(get_import_device_cache_key(5205, "other-server")) is None

    def test_http_fallback_populates_the_import_cache(self, librenms_api):
        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api, server = librenms_api
        server.device_info_response(device_id=5206, hostname="cache-after-live")

        result = fetch_device_with_cache(5206, api)

        assert result["hostname"] == "cache-after-live"
        assert cache.get(get_import_device_cache_key(5206, SERVER_KEY)) == result


@pytest.mark.django_db
class TestHostIpResolution:
    def test_single_real_interface_owner_is_resolved(self):
        from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip

        device = make_device("host-ip-single")
        ip_on(device, "198.18.10.9/24", "management")

        resolved, ambiguous, matching = resolve_device_by_host_ip("198.18.10.9")

        assert resolved == device
        assert ambiguous is False
        assert matching.count() == 1

    def test_device_and_oob_owners_for_the_same_host_fail_closed(self):
        from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip

        first = make_device("host-ip-first")
        ip_on(first, "198.18.10.10/24", "management")
        second = make_device("host-ip-second")
        second.oob_ip = make_ip("198.18.10.10/32")
        second.save()

        resolved, ambiguous, _matching = resolve_device_by_host_ip("198.18.10.10")

        assert resolved is None
        assert ambiguous is True

    def test_unassigned_address_has_no_owner(self):
        from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip

        make_ip("198.18.10.11/32")

        resolved, ambiguous, matching = resolve_device_by_host_ip("198.18.10.11")

        assert resolved is None
        assert ambiguous is False
        assert matching.count() == 1


@pytest.mark.django_db
class TestSerialRoleDecision:
    def _role(self, device, payload):
        from netbox_librenms_plugin.import_utils.device_operations import (
            _describe_existing_librenms_link,
            _detect_serial_match_role,
        )
        from netbox_librenms_plugin.utils import normalize_serial

        link = _describe_existing_librenms_link(device, SERVER_KEY)
        return _detect_serial_match_role(
            device,
            link,
            payload["hostname"],
            normalize_serial(payload.get("serial")),
            payload,
            SERVER_KEY,
        )

    def test_same_name_unlinked_device_is_a_plain_link(self):
        device = make_device("serial-link")
        result = self._role(device, _device_payload(5301, hostname=device.name, sysName=device.name))

        assert result["serial_action"] == "link"
        assert result["oob_candidate"] is None
        assert any("not linked" in warning for warning in result["warnings"])

    def test_incoming_controller_is_an_oob_candidate(self):
        device = make_device("serial-host")
        result = self._role(
            device,
            _device_payload(
                5302,
                hostname="serial-host-idrac",
                sysName="serial-host-idrac",
                hardware="iDRAC9",
                os="idrac",
            ),
        )

        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"]["device"] == device
        assert result["oob_candidate"]["type"] == "idrac"

    def test_existing_named_controller_can_be_promoted_to_host(self):
        device = make_device("serial-leaf-idrac", librenms_cf={SERVER_KEY: 99})
        result = self._role(
            device,
            _device_payload(7, hostname="serial-leaf", sysName="serial-leaf", hardware="server"),
        )

        assert result["serial_action"] == "promote_to_host"
        assert result["promote_to_host"]["existing_libre_id"] == 99
        assert result["serial_role_choice_available"] is True

    def test_existing_oob_link_turns_repeat_controller_into_information(self):
        device = make_device(
            "serial-linked-oob",
            librenms_cf={SERVER_KEY: {"id": 42, "oob": {"id": 99, "type": "idrac"}}},
        )
        result = self._role(
            device,
            _device_payload(7, hostname="serial-linked-oob-idrac", hardware="iDRAC9", os="idrac"),
        )

        assert result["serial_action"] == "oob_already_linked"
        assert any("already has an OOB controller" in warning for warning in result["warnings"])

    def test_unclassified_hostname_difference_is_a_reinstall_warning(self):
        device = make_device("serial-old-name")
        result = self._role(
            device,
            _device_payload(5303, hostname="serial-new-name", sysName="serial-new-name", hardware="server"),
        )

        assert result["serial_action"] == "hostname_differs"
        assert result["serial_role_choice_available"] is False

    def test_weak_role_signal_defaults_to_the_less_destructive_oob_action(self):
        device = make_device("serial-role-neutral", librenms_cf={SERVER_KEY: 99})

        result = self._role(
            device,
            _device_payload(5304, hostname="serial-role-renamed", hardware="server", os="linux"),
        )

        assert result["serial_action"] == "oob_candidate"
        assert result["serial_role_choice_available"] is True

    def test_unlinked_named_controller_can_only_be_an_oob_candidate(self):
        device = make_device("serial-role-bmc")

        result = self._role(
            device,
            _device_payload(5305, hostname="serial-role-host", hardware="server", os="linux"),
        )

        assert result["serial_action"] == "oob_candidate"
        assert result["serial_role_choice_available"] is False


@pytest.mark.django_db
class TestValidateDeviceForImport:
    def _validate(self, payload, **kwargs):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        kwargs.setdefault("include_vc_detection", False)
        return validate_device_for_import(payload, api=None, server_key=SERVER_KEY, **kwargs)

    def test_result_key_contract_and_new_device_blockers(self):
        result = self._validate(_device_payload(5401, hardware="-", location="-", serial="-", os="-"))

        assert set(result) == {
            "is_ready",
            "can_import",
            "import_as_vm",
            "resolved_name",
            "existing_device",
            "existing_match_type",
            "ambiguous_librenms_id",
            "serial_action",
            "serial_confirmed",
            "serial_duplicate",
            "serial_role_choice_available",
            "librenms_id_needs_migration",
            "oob_candidate",
            "existing_librenms_link",
            "merge_candidates",
            "name_matches",
            "name_sync_available",
            "suggested_name",
            "device_type_mismatch",
            "naming_criteria",
            "virtual_chassis",
            "issues",
            "warnings",
            "site",
            "region",
            "device_type",
            "device_role",
            "cluster",
            "platform",
            "rack",
            "location",
            "tenant",
        }
        assert result["existing_device"] is None
        assert result["can_import"] is False
        assert any("site" in issue.lower() for issue in result["issues"])
        assert any("device type" in issue.lower() for issue in result["issues"])
        assert any("role" in issue.lower() for issue in result["issues"])

    def test_naming_criteria_ignores_a_non_string_sysname(self):
        """naming_criteria reports the hostname source that the resolved name really came from."""
        payload = _device_payload(5402, sysName=7, hostname="sw02", hardware="-", location="-", serial="-", os="-")

        result = self._validate(payload, use_sysname=True)

        assert result["resolved_name"] == "sw02"
        criteria = result["naming_criteria"]
        assert criteria["source"] == "hostname"
        assert criteria["raw_sysname"] == ""
        assert criteria["raw_hostname"] == "sw02"

    def test_naming_criteria_reports_the_id_fallback_when_no_name_is_a_string(self):
        """With no string name at all, both the resolved name and the source are the id fallback."""
        payload = _device_payload(5403, sysName=7, hostname=None, hardware="-", location="-", serial="-", os="-")

        result = self._validate(payload)

        assert result["resolved_name"] == "device-5403"
        assert result["naming_criteria"]["source"] == "device-5403"

    def test_naming_criteria_reports_the_id_fallback_when_stripping_empties_the_name(self):
        """A sysName that strips to nothing resolves to device-<id>, and the source must say so."""
        payload = _device_payload(
            5404, sysName=".example.test", hostname="", hardware="-", location="-", serial="-", os="-"
        )

        result = self._validate(payload, use_sysname=True, strip_domain=True)

        assert result["resolved_name"] == "device-5404"
        assert result["naming_criteria"]["source"] == "device-5404"

    def test_real_site_type_roles_and_racks_are_exposed(self):
        from dcim.models import Rack

        infrastructure = make_device("validation-infrastructure")
        rack = Rack.objects.create(name="Validation rack", site=infrastructure.site, status="active")
        payload = _device_payload(
            5402,
            hostname="new-validation-device",
            sysName="new-validation-device",
            location=infrastructure.site.name,
            hardware=infrastructure.device_type.model,
        )

        result = self._validate(payload)

        assert result["site"]["site"] == infrastructure.site
        assert result["device_type"]["device_type"] == infrastructure.device_type
        assert infrastructure.role in result["device_role"]["available_roles"]
        assert rack in result["rack"]["available_racks"]
        assert result["rack"]["found"] is True
        assert result["cluster"]["found"] is True
        assert result["is_ready"] is False

    def test_new_vm_skips_device_fields_and_requires_a_cluster(self):
        cluster = make_cluster("Validation available cluster")

        result = self._validate(_device_payload(5403), import_as_vm=True)

        assert result["import_as_vm"] is True
        assert result["site"]["found"] is True
        assert result["device_type"]["found"] is True
        assert result["device_role"]["found"] is True
        assert cluster in result["cluster"]["available_clusters"]
        assert any("Cluster must be manually selected" in issue for issue in result["issues"])

    def test_device_id_match_forces_device_mode_and_surfaces_link(self):
        device = make_device("validation-linked", librenms_cf={SERVER_KEY: 5404})
        payload = _device_payload(
            5404,
            hostname=device.name,
            sysName=device.name,
            hardware=device.device_type.model,
            location=device.site.name,
            serial="-",
        )

        result = self._validate(payload, import_as_vm=True)

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "librenms_id"
        assert result["import_as_vm"] is False
        assert result["existing_librenms_link"]["host_id"] == 5404
        assert result["name_matches"] is True

    def test_vm_id_match_forces_vm_mode_and_populates_clusters(self):
        vm = make_vm("validation-linked-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 5405}
        vm.save()
        payload = _device_payload(5405, hostname=vm.name, sysName=vm.name, serial="-")

        result = self._validate(payload)

        assert result["existing_device"] == vm
        assert result["existing_match_type"] == "librenms_id"
        assert result["import_as_vm"] is True
        assert vm.cluster in result["cluster"]["available_clusters"]

    @pytest.mark.parametrize("legacy_value", [5406, " 5406 "])
    def test_legacy_id_matches_and_requests_migration(self, legacy_value):
        device = make_device("validation-legacy", librenms_cf=legacy_value)

        result = self._validate(_device_payload(5406, hostname=device.name, sysName=device.name, serial="-"))

        assert result["existing_device"] == device
        assert result["librenms_id_needs_migration"] is True

    def test_ambiguous_device_ids_are_terminal(self):
        first = make_device("validation-ambiguous-id-a", librenms_cf={SERVER_KEY: 5407})
        second = make_device("validation-ambiguous-id-b", librenms_cf={SERVER_KEY: 5407})

        result = self._validate(_device_payload(5407, hostname="unmatched-ambiguous", serial="-"))

        assert first != second
        assert result["existing_device"] is None
        assert result["ambiguous_librenms_id"] is True
        assert result["existing_match_type"] == "ambiguous_librenms_id"
        assert len(result["issues"]) == 1
        assert result["can_import"] is False

    def test_cross_model_id_collision_is_terminal(self):
        device = make_device("validation-cross-device", librenms_cf={SERVER_KEY: 5408})
        vm = make_vm("validation-cross-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 5408}
        vm.save()

        result = self._validate(_device_payload(5408, hostname="unmatched-cross", serial="-"))

        assert device.pk is not None and vm.pk is not None
        assert result["existing_device"] is None
        assert result["ambiguous_librenms_id"] is True

    def test_ambiguous_vm_ids_are_terminal(self):
        first = make_vm("validation-ambiguous-vm-a")
        second = make_vm("validation-ambiguous-vm-b", cluster=make_cluster("Validation VM ID cluster"))
        for vm in (first, second):
            vm.custom_field_data["librenms_id"] = {SERVER_KEY: 5421}
            vm.save()

        result = self._validate(_device_payload(5421, hostname="unmatched-vm-id", serial="-"))

        assert result["existing_device"] is None
        assert result["ambiguous_librenms_id"] is True
        assert result["existing_match_type"] == "ambiguous_librenms_id"

    def test_vm_id_with_ambiguous_device_owners_is_terminal(self):
        vm = make_vm("validation-vm-with-device-collision")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 5422}
        vm.save()
        make_device("validation-device-collision-a", librenms_cf={SERVER_KEY: 5422})
        make_device("validation-device-collision-b", librenms_cf={SERVER_KEY: 5422})

        result = self._validate(_device_payload(5422, hostname=vm.name, serial="-"))

        assert result["existing_device"] is None
        assert result["ambiguous_librenms_id"] is True

    def test_legacy_vm_id_and_name_drift_offer_both_migrations(self):
        vm = make_vm("validation-legacy-vm")
        vm.custom_field_data["librenms_id"] = " 5423 "
        vm.save()

        result = self._validate(_device_payload(5423, hostname="validation-renamed-vm", serial="-"))

        assert result["existing_device"] == vm
        assert result["librenms_id_needs_migration"] is True
        assert result["name_sync_available"] is True
        assert result["suggested_name"] == "edge-5423"

    def test_cross_model_hostname_is_not_bound_arbitrarily(self):
        device = make_device("validation-shared-name")
        vm = make_vm(device.name)

        result = self._validate(_device_payload(5409, hostname=device.name, sysName=device.name, serial="-"))

        assert vm.name == device.name
        assert result["existing_device"] is None
        assert any("Both a VM and Device" in warning for warning in result["warnings"])

    def test_vm_hostname_match_uses_the_real_vm_and_link_state(self):
        vm = make_vm("validation-hostname-vm")
        vm.custom_field_data["librenms_id"] = {SERVER_KEY: 99}
        vm.save()

        result = self._validate(
            _device_payload(5424, hostname=vm.name, sysName=vm.name, serial="-"),
            import_as_vm=False,
        )

        assert result["existing_device"] == vm
        assert result["existing_match_type"] == "hostname"
        assert result["import_as_vm"] is True
        assert result["existing_librenms_link"]["host_id"] == 99
        assert any("currently linked" in warning for warning in result["warnings"])

    def test_cross_model_hostname_with_duplicate_devices_is_terminal(self):
        from dcim.models import Device, Site

        first = make_device("validation-duplicate-cross-name")
        second_site = Site.objects.create(name="Validation duplicate site", slug="validation-duplicate-site")
        Device.objects.create(
            name=first.name,
            device_type=first.device_type,
            role=first.role,
            site=second_site,
            status="active",
        )
        make_vm(first.name)

        result = self._validate(_device_payload(5425, hostname=first.name, sysName=first.name, serial="-"))

        assert result["existing_device"] is None
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert any("Multiple NetBox devices share" in issue for issue in result["issues"])

    @pytest.mark.parametrize(
        ("incoming", "stored"),
        [
            (" SERIAL-TRIM ", "SERIAL-TRIM"),
            (123456, "123456"),
            (0, "0"),
        ],
    )
    def test_serial_matching_normalizes_real_payload_values(self, incoming, stored):
        device = make_device(f"serial-normalized-{stored}", serial=stored)
        payload = _device_payload(
            5410,
            hostname=f"unmatched-{stored}",
            sysName=f"unmatched-{stored}",
            serial=incoming,
            hardware="-",
            location="-",
        )

        result = self._validate(payload)

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "serial"
        assert not any("Validation error" in issue for issue in result["issues"])

    def test_linked_serial_match_confirms_trimmed_stored_value(self):
        device = make_device(
            "validation-linked-serial",
            serial=" SERIAL-LINKED ",
            librenms_cf={SERVER_KEY: 5411},
        )
        payload = _device_payload(
            5411,
            hostname=device.name,
            sysName=device.name,
            serial="SERIAL-LINKED",
            hardware=device.device_type.model,
            location=device.site.name,
        )

        result = self._validate(payload)

        assert result["serial_confirmed"] is True
        assert result["serial_action"] is None

    def test_linked_serial_drift_and_duplicate_are_distinguished(self):
        linked = make_device(
            "validation-serial-drift",
            serial="OLD-SERIAL",
            librenms_cf={SERVER_KEY: 5412},
        )
        payload = _device_payload(5412, hostname=linked.name, serial="NEW-SERIAL")

        drift = self._validate(payload)
        assert drift["serial_action"] == "update_serial"
        assert drift["serial_duplicate"] is False

        duplicate = make_device("validation-serial-owner", serial="DUPLICATE-SERIAL")
        payload["serial"] = duplicate.serial
        collision = self._validate(payload)
        assert collision["serial_action"] == "conflict"
        assert collision["serial_duplicate"] is True

    def test_hostname_matched_serial_drift_and_duplicate_are_distinguished(self):
        device = make_device("validation-hostname-serial-drift", serial="OLD-HOSTNAME-SERIAL")
        payload = _device_payload(
            5426,
            hostname=device.name,
            sysName=device.name,
            serial="NEW-HOSTNAME-SERIAL",
        )

        drift = self._validate(payload)
        assert drift["existing_match_type"] == "hostname"
        assert drift["serial_action"] == "update_serial"

        owner = make_device("validation-hostname-serial-owner", serial="OWNED-HOSTNAME-SERIAL")
        payload["serial"] = owner.serial
        collision = self._validate(payload)
        assert collision["serial_action"] == "conflict"
        assert collision["serial_duplicate"] is True

    def test_duplicate_serial_without_a_unique_owner_is_terminal(self):
        make_device("validation-duplicate-serial-a", serial="DUPLICATE-ONLY-SERIAL")
        make_device("validation-duplicate-serial-b", serial="DUPLICATE-ONLY-SERIAL")

        result = self._validate(
            _device_payload(5427, hostname="unmatched-duplicate-serial", serial="DUPLICATE-ONLY-SERIAL")
        )

        assert result["existing_device"] is None
        assert result["serial_duplicate"] is True
        assert any("Multiple NetBox devices share serial" in issue for issue in result["issues"])

    def test_primary_ip_match_forces_device_mode(self):
        device = make_device("validation-primary-ip")
        ip_on(device, "198.18.20.1/24", "management")
        payload = _device_payload(
            5413,
            hostname="different-primary-name",
            sysName="different-primary-name",
            serial="-",
            ip="198.18.20.1",
        )

        result = self._validate(payload, import_as_vm=True)

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "primary_ip"
        assert result["import_as_vm"] is False

    def test_vm_interface_primary_ip_forces_vm_mode(self):
        from virtualization.models import VMInterface

        vm = make_vm("validation-primary-ip-vm")
        interface = VMInterface.objects.create(virtual_machine=vm, name="management")
        make_ip("198.18.20.4/24", assigned_object=interface)

        result = self._validate(_device_payload(5428, hostname="unmatched-primary-vm", serial="-", ip="198.18.20.4"))

        assert result["existing_device"] == vm
        assert result["existing_match_type"] == "primary_ip"
        assert result["import_as_vm"] is True

    def test_ambiguous_primary_ip_is_terminal(self):
        first = make_device("validation-primary-a")
        second = make_device("validation-primary-b")
        ip_on(first, "198.18.20.2/24", "management")
        ip_on(second, "198.18.20.2/32", "management")
        payload = _device_payload(5414, hostname="unmatched-primary", serial="-", ip="198.18.20.2")

        result = self._validate(payload)

        assert result["existing_device"] is None
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert len(result["issues"]) == 1

    def test_unassigned_oob_ip_stages_an_oob_candidate(self):
        device = make_device("validation-oob-ip")
        device.oob_ip = make_ip("198.18.20.3/32")
        device.save()
        payload = _device_payload(
            5415,
            hostname="validation-oob-ip-idrac",
            sysName="validation-oob-ip-idrac",
            hardware="iDRAC9",
            os="idrac",
            serial="-",
            ip="198.18.20.3",
        )

        result = self._validate(payload)

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "primary_ip"
        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"]["type"] == "idrac"

    def test_linked_oob_ip_is_informational_instead_of_a_new_candidate(self):
        device = make_device(
            "validation-linked-oob-ip",
            librenms_cf={SERVER_KEY: {"id": 42, "oob": {"id": 99, "type": "idrac"}}},
        )
        device.oob_ip = make_ip("198.18.20.5/32")
        device.save()

        result = self._validate(
            _device_payload(
                5429,
                hostname="validation-linked-oob-ip-idrac",
                hardware="iDRAC9",
                os="idrac",
                serial="-",
                ip="198.18.20.5",
            )
        )

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "primary_ip"
        assert result["oob_candidate"] is None
        assert any("OOB already linked" in warning for warning in result["warnings"])

    def test_oob_id_reimport_is_not_a_host_serial_conflict(self):
        device = make_device(
            "validation-oob-linked",
            serial="HOST-SERIAL",
            librenms_cf={SERVER_KEY: {"id": 42, "oob": {"id": 5416, "type": "idrac"}}},
        )
        payload = _device_payload(
            5416,
            hostname="validation-oob-linked-idrac",
            sysName="validation-oob-linked-idrac",
            serial="CONTROLLER-SERIAL",
            hardware="iDRAC9",
            os="idrac",
        )

        result = self._validate(payload)

        assert result["existing_device"] == device
        assert result["existing_match_type"] == "librenms_oob"
        assert result["serial_action"] is None
        assert result["device_type_mismatch"] is False

    def test_hostname_and_serial_on_different_linked_devices_surface_merge_candidates(self):
        host = make_device(
            "validation-merge-host",
            serial="MERGE-SERIAL",
            librenms_cf={SERVER_KEY: {"id": 42}},
        )
        oob = make_device(
            "validation-merge-idrac",
            serial="MERGE-SERIAL",
            librenms_cf={SERVER_KEY: {"id": 99}},
        )
        payload = _device_payload(
            5417,
            hostname=host.name,
            sysName=host.name,
            serial="MERGE-SERIAL",
        )

        result = self._validate(payload)

        assert result["serial_action"] == "merge_netbox_devices"
        assert result["merge_candidates"]["host_named"]["pk"] == host.pk
        assert result["merge_candidates"]["oob_named"]["pk"] == oob.pk

    def test_collision_only_returns_identity_without_prerequisite_noise(self):
        result = self._validate(
            _device_payload(5418, hardware="-", location="-", serial="-", os="-"),
            collision_only=True,
        )

        assert result["existing_device"] is None
        assert result["issues"] == []
        assert result["device_role"]["available_roles"] == []

    def test_name_preferences_are_recorded_in_the_result(self):
        payload = _device_payload(
            5419,
            hostname="host.example.test",
            sysName="system.example.test",
            hardware="-",
            location="-",
            serial="-",
        )

        result = self._validate(payload, use_sysname=False, strip_domain=True)

        assert result["resolved_name"] == "host"
        assert result["naming_criteria"]["use_sysname"] is False
        assert result["naming_criteria"]["strip_domain"] is True
        assert result["naming_criteria"]["source"] == "hostname"

    def test_absent_names_record_the_generated_name_as_the_source(self):
        payload = _device_payload(5430, hardware="-", location="-", serial="-")
        payload.pop("hostname")
        payload.pop("sysName")

        result = self._validate(payload)

        assert result["resolved_name"] == "device-5430"
        assert result["naming_criteria"]["source"] == "device-5430"

    def test_non_string_names_fall_back_to_the_device_id(self):
        payload = _device_payload(5420, hostname=["bad"], sysName=123, hardware="-", location="-", serial="-")

        result = self._validate(payload)

        assert result["resolved_name"] == "device-5420"
        assert not any("Validation error" in issue for issue in result["issues"])

    def test_virtual_chassis_member_name_uses_the_real_pattern_and_serial(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings_row = LibreNMSSettings.objects.order_by("pk").first() or LibreNMSSettings()
        settings_row.vc_member_name_pattern = "-{serial}"
        settings_row.save()
        device = make_device(
            "validation-stack-master-SERIAL-VC-MEMBER",
            serial=" SERIAL-VC-MEMBER ",
            librenms_cf={SERVER_KEY: 5431},
        )
        make_virtual_chassis("validation-member-name-vc", device)

        result = self._validate(
            _device_payload(
                5431,
                hostname="validation-stack-master",
                sysName="validation-stack-master",
                serial="-",
                hardware=device.device_type.model,
                location=device.site.name,
            )
        )

        assert result["existing_device"] == device
        assert result["name_matches"] is True
        assert result["suggested_name"] is None

    def test_virtual_chassis_member_name_drift_offers_the_generated_name(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings_row = LibreNMSSettings.objects.order_by("pk").first() or LibreNMSSettings()
        settings_row.vc_member_name_pattern = "-{serial}"
        settings_row.save()
        device = make_device(
            "validation-old-member-name",
            serial="SERIAL-VC-DRIFT",
            librenms_cf={SERVER_KEY: 5432},
        )
        make_virtual_chassis("validation-member-drift-vc", device)

        result = self._validate(
            _device_payload(
                5432,
                hostname="validation-new-stack",
                sysName="validation-new-stack",
                serial="-",
                hardware=device.device_type.model,
                location=device.site.name,
            )
        )

        assert result["name_sync_available"] is True
        assert result["suggested_name"] == "validation-new-stack-SERIAL-VC-DRIFT"

    def test_ambiguous_device_types_are_a_terminal_prerequisite(self):
        from dcim.models import DeviceType, Manufacturer

        for suffix in ("a", "b"):
            manufacturer = Manufacturer.objects.create(
                name=f"Validation duplicate manufacturer {suffix}",
                slug=f"validation-duplicate-manufacturer-{suffix}",
            )
            DeviceType.objects.create(
                manufacturer=manufacturer,
                model="Validation shared hardware",
                slug=f"validation-shared-hardware-{suffix}",
            )

        result = self._validate(_device_payload(5433, hardware="Validation shared hardware", serial="-"))

        assert result["device_type"]["match_type"] == "ambiguous"
        assert any("Multiple device types match hardware" in issue for issue in result["issues"])

    def test_duplicate_platform_names_are_reported_as_ambiguous(self):
        from dcim.models import Manufacturer, Platform

        for suffix in ("a", "b"):
            manufacturer = Manufacturer.objects.create(
                name=f"Validation platform manufacturer {suffix}",
                slug=f"validation-platform-manufacturer-{suffix}",
            )
            Platform.objects.create(
                name="Validation shared OS",
                slug=f"validation-shared-os-{suffix}",
                manufacturer=manufacturer,
            )

        result = self._validate(_device_payload(5434, os="Validation shared OS", serial="-"))

        assert result["platform"]["match_type"] == "ambiguous"
        assert any("Multiple Platforms match OS" in warning for warning in result["warnings"])

    def test_linked_device_type_drift_is_reported(self):
        from dcim.models import DeviceType, Manufacturer

        device = make_device("validation-type-drift", librenms_cf={SERVER_KEY: 5435})
        manufacturer = Manufacturer.objects.create(
            name="Validation reported manufacturer",
            slug="validation-reported-manufacturer",
        )
        reported_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Validation reported type",
            slug="validation-reported-type",
        )

        result = self._validate(
            _device_payload(
                5435,
                hostname=device.name,
                sysName=device.name,
                hardware=reported_type.model,
                location=device.site.name,
                serial="-",
            )
        )

        assert result["device_type_mismatch"] is True
        assert any("Device type mismatch" in warning for warning in result["warnings"])


@pytest.mark.django_db
class TestValidationWithRealLibreNMS:
    def test_virtual_chassis_detection_uses_live_inventory(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        api, server = librenms_api
        infrastructure = make_device("vc-validation-infrastructure")
        payload = _device_payload(
            5501,
            hostname="vc-validation-master",
            sysName="vc-validation-master",
            serial="MEMBER-A",
            location=infrastructure.site.name,
            hardware=infrastructure.device_type.model,
        )
        server.device_info_response(device_id=5501, hostname=payload["hostname"], serial="MEMBER-A")
        server.vc_inventory_callable(
            5501,
            [_stack_root()],
            {
                100: [
                    _chassis(101, "MEMBER-A", 1),
                    _chassis(102, "MEMBER-B", 2),
                ]
            },
        )

        result = validate_device_for_import(payload, api=api, include_vc_detection=True)

        assert result["virtual_chassis"]["is_stack"] is True
        assert result["virtual_chassis"]["member_count"] == 2
        assert [member["position"] for member in result["virtual_chassis"]["members"]] == [1, 2]

    def test_chassis_inventory_recovers_an_unmatched_hardware_string(self, librenms_api):
        from dcim.models import DeviceType, Manufacturer
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        api, server = librenms_api
        infrastructure = make_device("chassis-validation-infrastructure")
        manufacturer = Manufacturer.objects.create(name="Validation chassis manufacturer", slug="validation-chassis")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Validation chassis model",
            slug="validation-chassis-model",
            part_number="VALIDATION-CHASSIS-PN",
        )
        server.register(
            "/api/v0/inventory/5502",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalName": "VALIDATION-CHASSIS-PN",
                        "entPhysicalModelName": "",
                    }
                ],
            },
        )
        payload = _device_payload(
            5502,
            hostname="chassis-validation-new",
            sysName="chassis-validation-new",
            hardware="Unmatched hardware value",
            location=infrastructure.site.name,
        )

        result = validate_device_for_import(payload, api=api, include_vc_detection=False)

        assert result["device_type"]["device_type"] == device_type
        assert result["device_type"]["match_type"] == "chassis"


@pytest.mark.django_db
class TestImportSingleDevice:
    def _infrastructure(self, tag):
        device = make_device(f"import-infrastructure-{tag}")
        return device.site, device.device_type, device.role

    def test_missing_required_mappings_fail_in_order(self, settings, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        site, device_type, role = self._infrastructure("missing")
        payload = _device_payload(5601)

        missing_site = import_single_device(5601, SERVER_KEY, _validation(), libre_device=payload)
        missing_type = import_single_device(5601, SERVER_KEY, _validation(site=site), libre_device=payload)
        missing_role = import_single_device(
            5601,
            SERVER_KEY,
            _validation(site=site, device_type=device_type),
            libre_device=payload,
        )

        assert missing_site["error"] == "Site is required but not provided"
        assert missing_type["error"] == "Device type is required but not provided"
        assert missing_role["error"] == "Device role is required but not provided"
        assert role is not None

    def test_real_import_persists_normalized_identity_and_location(self, librenms_api):
        from dcim.models import Location
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.utils import get_librenms_device_id

        _api, _server = librenms_api
        site, device_type, role = self._infrastructure("success")
        location = Location.objects.create(
            name="Import location",
            slug="import-location",
            site=site,
            status="active",
        )
        payload = _device_payload(
            5602,
            hostname="imported.example.test",
            sysName="imported.example.test",
            serial=" SERIAL-5602 ",
            location=location.name,
            status=1,
        )
        validation = _validation(
            site=site,
            device_type=device_type,
            role=role,
            resolved_name="imported-edge-5602",
        )

        result = import_single_device(
            5602,
            SERVER_KEY,
            validation,
            sync_options={"sync_interfaces": False, "sync_cables": False},
            libre_device=payload,
        )

        assert result["success"] is True
        assert result["device"].name == "imported-edge-5602"
        assert result["device"].serial == "SERIAL-5602"
        assert result["device"].location == location
        assert result["device"].status == "active"
        assert get_librenms_device_id(result["device"], SERVER_KEY) == 5602
        assert result["synced"] == {"interfaces": 0, "cables": 0, "ip_addresses": 0}

    def test_manual_mappings_use_real_objects(self, librenms_api):
        from dcim.models import Platform, Rack
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        site, device_type, role = self._infrastructure("manual")
        platform = Platform.objects.create(name="Import platform", slug="import-platform")
        rack = Rack.objects.create(name="Import manual rack", site=site, status="active")

        result = import_single_device(
            5603,
            SERVER_KEY,
            _validation(resolved_name="manual-import"),
            manual_mappings={
                "site_id": site.pk,
                "device_type_id": device_type.pk,
                "device_role_id": role.pk,
                "platform_id": platform.pk,
                "rack_id": rack.pk,
            },
            libre_device=_device_payload(5603),
        )

        assert result["success"] is True
        assert result["device"].site == site
        assert result["device"].device_type == device_type
        assert result["device"].role == role
        assert result["device"].platform == platform
        assert result["device"].rack == rack

    def test_existing_and_ambiguous_validation_states_block_creation(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        existing = make_device("import-existing")
        site, device_type, role = self._infrastructure("blocked")
        payload = _device_payload(5604)

        existing_result = import_single_device(
            5604,
            SERVER_KEY,
            _validation(
                site=site,
                device_type=device_type,
                role=role,
                existing_device=existing,
            ),
            libre_device=payload,
        )
        id_ambiguity = import_single_device(
            5604,
            SERVER_KEY,
            _validation(
                site=site,
                device_type=device_type,
                role=role,
                ambiguous_librenms_id=True,
            ),
            libre_device=payload,
        )
        identity_ambiguity = import_single_device(
            5604,
            SERVER_KEY,
            _validation(
                site=site,
                device_type=device_type,
                role=role,
                existing_match_type="ambiguous_hostname_or_serial",
            ),
            libre_device=payload,
        )

        assert "already exists" in existing_result["error"]
        assert "ambiguous LibreNMS ID" in id_ambiguity["error"]
        assert "matches multiple NetBox devices" in identity_ambiguity["error"]

    def test_assignment_conflict_is_checked_again_under_the_real_lock(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        owner = make_device("import-id-owner", librenms_cf={SERVER_KEY: 5605})
        site, device_type, role = self._infrastructure("conflict")

        result = import_single_device(
            5605,
            SERVER_KEY,
            _validation(site=site, device_type=device_type, role=role),
            libre_device=_device_payload(5605),
        )

        assert result["success"] is False
        assert f"device '{owner.name}'" in result["error"]

    def test_vm_assignment_conflict_is_identified_as_a_vm(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        owner = make_vm("import-vm-id-owner")
        owner.custom_field_data["librenms_id"] = {SERVER_KEY: 5609}
        owner.save()
        site, device_type, role = self._infrastructure("vm-conflict")

        result = import_single_device(
            5609,
            SERVER_KEY,
            _validation(site=site, device_type=device_type, role=role),
            libre_device=_device_payload(5609),
        )

        assert result["success"] is False
        assert f"VM '{owner.name}'" in result["error"]

    def test_empty_resolved_name_recomputes_from_sync_preferences(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, _server = librenms_api
        site, device_type, role = self._infrastructure("fallback-name")

        result = import_single_device(
            5610,
            SERVER_KEY,
            _validation(site=site, device_type=device_type, role=role, resolved_name=""),
            sync_options={
                "sync_interfaces": False,
                "sync_cables": False,
                "use_sysname": False,
                "strip_domain": True,
            },
            libre_device=_device_payload(
                5610,
                hostname="fallback-import.example.test",
                sysName="ignored-system.example.test",
            ),
        )

        assert result["success"] is True
        assert result["device"].name == "fallback-import"

    def test_unknown_server_key_returns_a_scoped_import_error(self, settings):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config

        result = import_single_device(5611, "missing-server", libre_device=_device_payload(5611))

        assert result["success"] is False
        assert result["device"] is None
        assert "missing-server" in result["error"]

    def test_live_fetch_is_used_when_no_payload_is_supplied(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, server = librenms_api
        site, device_type, role = self._infrastructure("live")
        server.device_info_response(
            device_id=5606,
            hostname="live-import",
            hardware=device_type.model,
            serial="LIVE-5606",
            location=site.name,
        )

        result = import_single_device(
            5606,
            SERVER_KEY,
            _validation(site=site, device_type=device_type, role=role, resolved_name="live-import"),
        )

        assert result["success"] is True
        assert result["device"].name == "live-import"
        assert result["device"].serial == "LIVE-5606"

    def test_missing_live_device_returns_a_scoped_error(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, server = librenms_api
        server.register("/api/v0/devices/5607", {"status": "error"}, status=404)

        result = import_single_device(5607, SERVER_KEY)

        assert result["success"] is False
        assert result["error"] == "Failed to retrieve device 5607 from LibreNMS"

    def test_lazy_validation_runs_through_the_real_api_and_database(self, librenms_api):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        _api, server = librenms_api
        infrastructure = make_device("lazy-import-infrastructure")
        server.device_info_response(
            device_id=5608,
            hostname="lazy-import",
            hardware=infrastructure.device_type.model,
            serial="LAZY-5608",
            location=infrastructure.site.name,
        )
        server.vc_inventory_callable(5608, [], {})

        result = import_single_device(5608, SERVER_KEY)

        assert result["success"] is False
        assert result["device"] is None
        assert result["error"] == "Device role is required but not provided"

    def _import_location_payload(self, location, *, pattern, validation, manual_mappings=None):
        """Import through the real parser and reload the persisted device."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={"location_parse_pattern": pattern, "location_parse_is_regex": False},
        )
        result = import_single_device(
            5620,
            SERVER_KEY,
            validation,
            manual_mappings=manual_mappings,
            sync_options={"sync_interfaces": False, "sync_cables": False},
            libre_device=_device_payload(5620, location=location, serial="-"),
        )
        assert result["success"] is True, result
        result["device"].refresh_from_db()
        return result["device"]

    def test_rack_resolved_from_parsed_token_exact_match(self, librenms_api):
        """Persist the rack matched by name within the imported device's site."""
        from dcim.models import Rack

        site, device_type, role = self._infrastructure("parsed-rack")
        rack = Rack.objects.create(name="R1", site=site, status="active")

        device = self._import_location_payload(
            f"{site.name}, R1",
            pattern="{site}, {rack}",
            validation=_validation(site=site, device_type=device_type, role=role),
        )

        assert device.site == site
        assert device.rack == rack

    def test_rack_resolved_from_parsed_token_via_mapping(self, librenms_api):
        """Persist the mapped rack when the parsed token has no exact match."""
        from dcim.models import Rack
        from netbox_librenms_plugin.models import LocationMapping

        site, device_type, role = self._infrastructure("mapped-rack")
        rack = Rack.objects.create(name="Mapped Rack", site=site, status="active")
        LocationMapping.objects.create(field_type="rack", librenms_value="R1", netbox_object=rack)

        device = self._import_location_payload(
            f"{site.name}, R1",
            pattern="{site}, {rack}",
            validation=_validation(site=site, device_type=device_type, role=role),
        )

        assert device.rack == rack

    def test_ambiguous_rack_name_skips_automatic_assignment(self, librenms_api, caplog):
        """Leave the rack unset when duplicate names exist, even if an alias matches."""
        from dcim.models import Location, Rack
        from netbox_librenms_plugin.models import LocationMapping

        site, device_type, role = self._infrastructure("ambiguous-rack")
        first_location = Location.objects.create(name="Hall A", slug="hall-a", site=site)
        second_location = Location.objects.create(name="Hall B", slug="hall-b", site=site)
        first_rack = Rack.objects.create(name="R1", site=site, location=first_location, status="active")
        Rack.objects.create(name="R1", site=site, location=second_location, status="active")
        LocationMapping.objects.create(field_type="rack", librenms_value="R1", netbox_object=first_rack)

        device = self._import_location_payload(
            f"{site.name}, R1",
            pattern="{site}, {rack}",
            validation=_validation(site=site, device_type=device_type, role=role),
        )

        assert device.rack is None
        assert "Multiple racks named" in caplog.text

    def test_automatic_rack_is_reresolved_after_manual_site_override(self, librenms_api):
        """Resolve the rack within the selected site instead of keeping the suggested rack."""
        from dcim.models import Rack, Site

        site, device_type, role = self._infrastructure("overridden-site")
        original_rack = Rack.objects.create(name="R9", site=site, status="active")
        selected_site = Site.objects.create(name="Selected Site", slug="selected-site")
        selected_rack = Rack.objects.create(name="R9", site=selected_site, status="active")

        device = self._import_location_payload(
            f"{site.name}, R9",
            pattern="{site}, {rack}",
            validation=_validation(site=site, device_type=device_type, role=role, rack={"rack": original_rack}),
            manual_mappings={"site_id": selected_site.pk},
        )

        assert device.site == selected_site
        assert device.rack == selected_rack

    def test_tenant_resolved_from_parsed_token_exact_match(self, librenms_api):
        """Persist the tenant matched by its parsed name."""
        from tenancy.models import Tenant

        site, device_type, role = self._infrastructure("parsed-tenant")
        tenant = Tenant.objects.create(name="Example Tenant", slug="example-tenant")

        device = self._import_location_payload(
            f"{site.name}, {tenant.name}",
            pattern="{site}, {tenant}",
            validation=_validation(site=site, device_type=device_type, role=role),
        )

        assert device.tenant == tenant

    def test_tenant_resolved_from_parsed_token_via_mapping(self, librenms_api):
        """Persist the mapped tenant when the parsed token has no exact match."""
        from tenancy.models import Tenant
        from netbox_librenms_plugin.models import LocationMapping

        site, device_type, role = self._infrastructure("mapped-tenant")
        tenant = Tenant.objects.create(name="Example Tenant", slug="example-tenant")
        LocationMapping.objects.create(field_type="tenant", librenms_value="Tenant Alias", netbox_object=tenant)

        device = self._import_location_payload(
            f"{site.name}, Tenant Alias",
            pattern="{site}, {tenant}",
            validation=_validation(site=site, device_type=device_type, role=role),
        )

        assert device.tenant == tenant


def test_lazy_bulk_import_export_and_unknown_attributes():
    import netbox_librenms_plugin.import_utils.device_operations as operations

    assert callable(operations.bulk_import_devices_shared)
    with pytest.raises(AttributeError):
        getattr(operations, "unknown_device_operation")
