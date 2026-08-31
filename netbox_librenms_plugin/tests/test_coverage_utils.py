"""Behavior tests for utility functions against real NetBox objects."""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
)


pytestmark = pytest.mark.django_db


def _set_mapping(obj, value):
    obj.custom_field_data["librenms_id"] = value
    obj.save(update_fields=["custom_field_data"])


def _add_primary_ip(device, tag):
    interface = make_interface(device, f"management-{tag}")
    address = make_ip(f"198.18.{device.pk % 200}.1/24", assigned_object=interface)
    device.primary_ip4 = address
    device.save(update_fields=["primary_ip4"])
    return address


@pytest.mark.django_db
class TestSerialScopeNormalization:
    """
    Juniper prefixes ENTITY-MIB serials with a literal "S/N ".

    The rewrite is a NormalizationRule rather than compiled-in, so an operator can see why a
    stored serial differs from the raw inventory and add the next vendor without a release.
    """

    def test_the_migration_seeds_the_juniper_rule(self):
        from netbox_librenms_plugin.models import NormalizationRule

        assert NormalizationRule.objects.filter(
            scope=NormalizationRule.SCOPE_SERIAL, match_pattern=r"^S/N\s+(.+)$"
        ).exists()

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("S/N BCFB9793", "BCFB9793"),
            ("  S/N BCFB9751  ", "BCFB9751"),
            ("BCFB9793", "BCFB9793"),
            ("SN12345", "SN12345"),
            ("S/NABC", "S/NABC"),
            (12345, "12345"),
            (0, "0"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_the_seeded_rule_strips_only_the_marker(self, raw, expected):
        from netbox_librenms_plugin.utils import normalize_inventory_serial

        assert normalize_inventory_serial(raw) == expected

    def test_normalize_serial_itself_no_longer_strips(self):
        """The transformation lives in the rule, so there is one place to look."""
        from netbox_librenms_plugin.utils import normalize_serial

        assert normalize_serial("S/N BCFB9793") == "S/N BCFB9793"

    def test_disabling_the_rule_stops_the_rewrite(self):
        """Proves the stored serial follows the rule rather than compiled-in behaviour."""
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import normalize_inventory_serial

        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()

        assert normalize_inventory_serial("S/N BCFB9793") == "S/N BCFB9793"


class TestConvertSpeedToKbps:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), (0, 0), (1, 0), (999, 0), (1_000, 1), (1_000_000_000, 1_000_000)],
    )
    def test_boundaries(self, value, expected):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(value) == expected

    def test_non_numeric_value_raises_type_error(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        with pytest.raises(TypeError):
            convert_speed_to_kbps("1000")


class TestGetVirtualChassisMember:
    def test_resolves_real_members_by_prefixed_and_slot_style_names(self):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        first = make_device("vc-port-first")
        second = make_device("vc-port-second")
        make_virtual_chassis("vc-port-resolution", first, second)

        assert get_virtual_chassis_member(first, "Ethernet2/1") == second
        assert get_virtual_chassis_member(first, "2/1/1") == second
        assert get_virtual_chassis_member(first, "Ethernet2/1", members_by_position={2: second}) == second

    @pytest.mark.parametrize("port_name", ["Management", None, 42])
    def test_unusable_name_returns_the_original_device(self, port_name):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        first = make_device(f"vc-port-fallback-{port_name}")
        second = make_device(f"vc-port-fallback-member-{port_name}")
        make_virtual_chassis(f"vc-port-fallback-{port_name}", first, second)

        assert get_virtual_chassis_member(first, port_name) == first
        assert get_virtual_chassis_member(first, port_name, return_device_on_failure=False) is None

    def test_missing_position_and_empty_prefetch_map_fall_back_cleanly(self):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        first = make_device("vc-port-missing-first")
        second = make_device("vc-port-missing-second")
        make_virtual_chassis("vc-port-missing", first, second)

        assert get_virtual_chassis_member(first, "Ethernet9") == first
        assert get_virtual_chassis_member(first, "Ethernet2", members_by_position={}) == second


class TestGetLibreNMSSyncDevice:
    def _members(self, tag, count=3):
        members = [make_device(f"vc-sync-{tag}-{index}") for index in range(1, count + 1)]
        vc = make_virtual_chassis(f"vc-sync-{tag}", *members)
        return vc, members

    def test_explicit_server_mapping_has_highest_priority(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        _vc, (first, second, _third) = self._members("mapping")
        _set_mapping(first, {"other": 10})
        _set_mapping(second, {"default": {"id": "42"}})

        assert get_librenms_sync_device(first, server_key="default") == second
        assert get_librenms_sync_device(first, server_key=None) == first

    def test_legacy_mapping_is_a_server_fallback(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        _vc, (first, second, _third) = self._members("legacy")
        _set_mapping(second, 55)

        assert get_librenms_sync_device(first, server_key="default") == second

    def test_float_mapping_is_rejected_in_favor_of_a_valid_mapping(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        _vc, (first, second, third) = self._members("float")
        _set_mapping(first, {"default": 1.0})
        _set_mapping(third, {"other": 5})

        assert get_librenms_sync_device(first, server_key=None) == third

    def test_master_then_any_primary_ip_then_lowest_position_fallbacks(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        vc, (first, second, third) = self._members("fallback")
        _add_primary_ip(second, "second")
        vc.master = second
        vc.save(update_fields=["master"])
        assert get_librenms_sync_device(first, server_key="default") == second

        vc.master = None
        vc.save(update_fields=["master"])
        assert get_librenms_sync_device(first, server_key="default") == second

        second.primary_ip4 = None
        second.save(update_fields=["primary_ip4"])
        assert get_librenms_sync_device(third, server_key="default") == first

    def test_standalone_device_is_its_own_sync_device(self):
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = make_device("sync-standalone")
        assert get_librenms_sync_device(device, server_key="default") == device


class TestPaginationAndUserPreferences:
    @pytest.mark.parametrize("raw", ["not-a-number", "0", "-5"])
    def test_invalid_table_page_size_uses_the_real_netbox_default(self, raw):
        from django.contrib.auth.models import AnonymousUser

        from netbox_librenms_plugin.utils import get_table_paginate_count, netbox_get_paginate_count

        request = RequestFactory().get("/", {"table_per_page": raw})
        request.user = AnonymousUser()

        assert get_table_paginate_count(request, "table_") == netbox_get_paginate_count(request)

    def test_valid_table_page_size_is_read_from_get_and_post(self):
        from netbox_librenms_plugin.utils import get_table_paginate_count

        get_request = RequestFactory().get("/", {"table_per_page": "17"})
        post_request = RequestFactory().post("/", {"table_per_page": "19"})

        assert get_table_paginate_count(get_request, "table_") == 17
        assert get_table_paginate_count(post_request, "table_") == 19

    def test_real_user_preference_round_trip(self):
        from netbox_librenms_plugin.utils import get_user_pref, save_user_pref

        request = RequestFactory().get("/")
        request.user = make_superuser("utils-preferences")

        save_user_pref(request, "netbox_librenms_plugin.tests.choice", "ifDescr")

        assert get_user_pref(request, "netbox_librenms_plugin.tests.choice") == "ifDescr"

    def test_missing_user_or_config_returns_the_default(self):
        from netbox_librenms_plugin.utils import get_user_pref

        assert get_user_pref(SimpleNamespace(), "missing", default="fallback") == "fallback"
        assert get_user_pref(SimpleNamespace(user=SimpleNamespace()), "missing", default="fallback") == "fallback"


class TestMatchLibreNMSHardware:
    @staticmethod
    def _device_type(tag, *, model=None, part_number=""):
        from dcim.models import DeviceType, Manufacturer

        manufacturer = Manufacturer.objects.create(name=f"Hardware {tag}", slug=f"hardware-{tag}")
        return DeviceType.objects.create(
            manufacturer=manufacturer,
            model=model or f"MODEL-{tag}",
            slug=f"model-{tag}",
            part_number=part_number,
        )

    @pytest.mark.parametrize("hardware", ["", "-", "   "])
    def test_empty_hardware_does_not_match(self, hardware):
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type(hardware)
        assert result["matched"] is False

    def test_real_mapping_and_normalization_rule_match(self):
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        device_type = self._device_type("normalized", model="C9300-48P")
        DeviceTypeMapping.objects.create(librenms_hardware="C9300-48P", netbox_device_type=device_type)
        NormalizationRule.objects.create(
            scope="device_type",
            match_pattern=r"^WS-(.+)$",
            replacement=r"\1",
            priority=10,
        )

        result = match_librenms_hardware_to_device_type("WS-C9300-48P")

        assert result == {"matched": True, "device_type": device_type, "match_type": "mapping"}

    def test_wrong_rule_scope_does_not_change_device_type_lookup(self):
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        device_type = self._device_type("wrong-scope", model="SCOPE-MODEL")
        DeviceTypeMapping.objects.create(librenms_hardware="SCOPE-MODEL", netbox_device_type=device_type)
        NormalizationRule.objects.create(
            scope="module_type",
            match_pattern=r"^WS-(.+)$",
            replacement=r"\1",
            priority=10,
        )

        assert match_librenms_hardware_to_device_type("SCOPE-MODEL")["device_type"] == device_type
        assert match_librenms_hardware_to_device_type("WS-SCOPE-MODEL")["matched"] is False

    def test_exact_part_number_and_model_paths_use_real_queries(self):
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        by_part = self._device_type("part", model="PART-MODEL", part_number="PART-123")
        by_model = self._device_type("model", model="EXACT-MODEL")

        assert match_librenms_hardware_to_device_type("PART-123")["device_type"] == by_part
        assert match_librenms_hardware_to_device_type("EXACT-MODEL")["device_type"] == by_model

    @pytest.mark.parametrize("field", ["part_number", "model"])
    def test_case_insensitive_ambiguity_fails_closed(self, field):
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        value = f"AMBIGUOUS-{field}"
        kwargs = {field: value}
        self._device_type(f"{field}-one", **kwargs)
        self._device_type(f"{field}-two", **{field: value.lower()})

        assert match_librenms_hardware_to_device_type(value) is None

    def test_preloaded_normalization_rules_avoid_the_per_call_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type, preload_normalization_rules

        NormalizationRule.objects.create(
            scope="device_type",
            match_pattern=r"^WS-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        preloaded = preload_normalization_rules("device_type")

        with CaptureQueriesContext(connection) as captured:
            match_librenms_hardware_to_device_type("WS-NO-MATCH", preloaded_rules=preloaded)

        assert not any("normalizationrule" in query["sql"].lower() for query in captured.captured_queries)

    def test_chassis_fallback_uses_real_http_and_preloaded_rules(self, live_librenms):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import preload_normalization_rules

        device_type = self._device_type("chassis", model="CHASSIS-MODEL")
        DeviceTypeMapping.objects.create(librenms_hardware="CHASSIS-MODEL", netbox_device_type=device_type)
        NormalizationRule.objects.create(
            scope="device_type",
            match_pattern=r"^WS-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        live_librenms.server.register(
            "/api/v0/inventory/123",
            {"status": "ok", "inventory": [{"entPhysicalName": "WS-CHASSIS-MODEL"}]},
            method="GET",
        )

        result = _try_chassis_device_type_match(
            live_librenms.api,
            123,
            preloaded_device_type_rules=preload_normalization_rules("device_type"),
        )

        assert result["device_type"] == device_type
        assert result["match_type"] == "chassis"
        assert live_librenms.server.requests[0]["query"] == {"entPhysicalClass": ["chassis"]}


class TestLocationAndPlatformMatching:
    def test_case_insensitive_duplicate_site_uses_the_first_deterministic_row(self):
        from dcim.models import Site

        from netbox_librenms_plugin.utils import find_matching_site

        first = Site.objects.create(name="Example Site", slug="example-site-one")
        Site.objects.create(name="EXAMPLE SITE", slug="example-site-two")

        result = find_matching_site("example site")

        assert result["found"] is True
        assert result["site"] == first

    def test_case_insensitive_duplicate_platform_fails_closed(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.utils import find_matching_platform

        Platform.objects.create(name="example-os", slug="example-os")
        Platform.objects.create(name="EXAMPLE-OS", slug="example-os-upper")

        assert find_matching_platform("example-os") == {
            "found": False,
            "platform": None,
            "match_type": "ambiguous",
            "ambiguity_source": "platform",
        }


class TestStoredLibreNMSIdentifiers:
    @pytest.mark.parametrize(
        ("stored", "server_key", "expected", "persisted"),
        [
            ("42", "default", 42, 42),
            ({"default": "77"}, "default", 77, {"default": 77}),
            ({"default": {"id": "88"}}, "default", 88, {"default": {"id": 88}}),
            ("not-a-number", "default", None, "not-a-number"),
        ],
    )
    def test_real_object_normalization_and_persistence(self, stored, server_key, expected, persisted):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        device = make_device(f"stored-id-{expected}")
        _set_mapping(device, stored)

        assert get_librenms_device_id(device, server_key) == expected
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == persisted

    def test_read_only_lookup_normalizes_without_persisting(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        device = make_device("stored-id-read-only")
        _set_mapping(device, {"default": "99"})

        assert get_librenms_device_id(device, "default", auto_save=False) == 99
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == {"default": "99"}

    def test_find_by_none_short_circuits_and_locked_lookup_finds_the_owner(self):
        from dcim.models import Device
        from django.db import transaction

        from netbox_librenms_plugin.utils import find_by_librenms_id

        assert find_by_librenms_id(Device, None, server_key="default") is None
        owner = make_device("stored-id-owner", librenms_cf={"default": 4242})
        with transaction.atomic():
            found = find_by_librenms_id(Device, 4242, server_key="default", select_for_update=True)
        assert found == owner


class TestSmallRenderingAndShapeHelpers:
    def test_missing_vlan_warning_only_marks_missing_vids(self):
        from netbox_librenms_plugin.utils import get_missing_vlan_warning

        assert "mdi-alert" in get_missing_vlan_warning(100, [100, 200])
        assert get_missing_vlan_warning(999, [100, 200]) == ""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [(42, True), (" 42 ", True), (True, False), ({"default": 42}, False), (None, False), ("abc", False)],
    )
    def test_legacy_identifier_shape(self, stored, expected):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id(stored) is expected


class TestNetBoxVersionGates:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [((4, 5, 6), True), ((4, 6, 0), True), ((4, 5, 5), False), ((4, 4, 10), False), (None, True)],
    )
    def test_module_token_leaf_gate(self, monkeypatch, version, expected):
        from netbox_librenms_plugin import utils

        monkeypatch.setattr(utils, "_get_netbox_version_tuple", lambda: version)
        assert utils.netbox_resolves_module_token_per_leaf() is expected

    @pytest.mark.parametrize(
        ("version", "expected"),
        [((4, 4, 0), True), ((4, 4, 1), False), ((4, 6, 5), False), ((4, 3, 9), False), (None, True)],
    )
    def test_parent_virtual_chassis_clean_gate(self, monkeypatch, version, expected):
        from netbox_librenms_plugin import utils

        monkeypatch.setattr(utils, "_get_netbox_version_tuple", lambda: version)
        assert utils.netbox_clean_reads_parent_virtual_chassis() is expected

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ((4, 4, 0), False),
            ((4, 5, 5), False),
            ((4, 6, 0), True),
            ((4, 6, 5), True),
            (None, False),
        ],
    )
    def test_standalone_vm_host_gate(self, monkeypatch, version, expected):
        from netbox_librenms_plugin import utils

        monkeypatch.setattr(utils, "_get_netbox_version_tuple", lambda: version)
        assert utils.netbox_allows_standalone_vm_host() is expected

    @pytest.mark.parametrize(
        ("version", "expected"),
        [("4.5.8", (4, 5, 8)), ("4.5.8-Docker-4.0.2", (4, 5, 8)), ("not-a-version", None)],
    )
    def test_release_version_parsing(self, monkeypatch, version, expected):
        from netbox import settings as netbox_settings
        from netbox_librenms_plugin import utils

        monkeypatch.setattr(netbox_settings, "RELEASE", SimpleNamespace(version=version))
        assert utils._get_netbox_version_tuple() == expected


class TestNestedModuleNameConflict:
    def _real_nested_bay(self, tag, template_name):
        from dcim.models import InterfaceTemplate, Module, ModuleBay

        device = make_device(f"nested-{tag}")
        carrier_type = make_module_type(f"CARRIER-{tag}")
        carrier_bay = make_module_bay(device, f"Carrier Bay {tag}")
        carrier = Module.objects.create(device=device, module_bay=carrier_bay, module_type=carrier_type)
        nested_bay = ModuleBay.objects.create(device=device, module=carrier, name=f"Nested Bay {tag}")
        tested_type = make_module_type(f"TESTED-{tag}")
        InterfaceTemplate.objects.create(module_type=tested_type, name=template_name, type="other")
        return tested_type, nested_bay, carrier

    def test_old_netbox_warns_for_sibling_module_token_collision(self, monkeypatch):
        from netbox_librenms_plugin import utils

        module_type, module_bay, carrier = self._real_nested_bay("warning", "{module}")
        monkeypatch.setattr(utils, "netbox_resolves_module_token_per_leaf", lambda: False)

        reason = utils.has_nested_name_conflict(module_type, module_bay, {carrier.pk: 2})

        assert module_type.model in reason
        assert "4.5.6" in reason

    @pytest.mark.parametrize(
        ("template_name", "sibling_count", "fixed", "expected"),
        [("{module}", 2, True, ""), ("Ethernet1", 2, False, ""), ("{module}", 1, False, "")],
    )
    def test_nonconflicting_nested_cases(self, monkeypatch, template_name, sibling_count, fixed, expected):
        from netbox_librenms_plugin import utils

        module_type, module_bay, carrier = self._real_nested_bay(
            f"{template_name}-{sibling_count}-{fixed}",
            template_name,
        )
        monkeypatch.setattr(utils, "netbox_resolves_module_token_per_leaf", lambda: fixed)

        assert utils.has_nested_name_conflict(module_type, module_bay, {carrier.pk: sibling_count}) == expected


class TestValidateRegexField:
    def test_valid_and_invalid_patterns(self):
        import re

        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.utils import validate_regex_field

        assert isinstance(validate_regex_field(r"^Po\d+$", "pattern"), re.Pattern)
        with pytest.raises(ValidationError) as error:
            validate_regex_field("[unbalanced(", "pattern")
        assert "pattern" in error.value.message_dict

    def test_real_mapping_model_routes_bad_regex_through_the_shared_validator(self):
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping(
            librenms_name="[unbalanced(",
            librenms_class="module",
            netbox_bay_name="Slot 1",
            is_regex=True,
        )

        with pytest.raises(ValidationError) as error:
            mapping.full_clean()
        assert "librenms_name" in error.value.message_dict


class TestInterfaceNameFallbackMatchesPort:
    def _interface(self, stored):
        device = make_device(f"interface-fallback-{stored}")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        if stored is not None:
            _set_mapping(interface, stored)
        return interface

    @pytest.mark.parametrize(
        "stored",
        [
            {"default": 42},
            {"default": "42"},
            {"default": {"id": 42}},
            {"default": {"id": "42"}},
            42,
            "42",
        ],
    )
    def test_agrees_with_the_shared_identifier_reader(self, stored):
        from netbox_librenms_plugin.utils import get_librenms_device_id, interface_name_fallback_matches_port

        interface = self._interface(stored)
        assert get_librenms_device_id(interface, "default", auto_save=False) == 42
        assert interface_name_fallback_matches_port(interface, 42, "default") is True
        assert interface_name_fallback_matches_port(interface, 43, "default") is False

    def test_unbound_and_other_server_entries_are_available(self):
        from netbox_librenms_plugin.utils import interface_name_fallback_matches_port

        assert interface_name_fallback_matches_port(self._interface(None), 42, "default") is True
        assert interface_name_fallback_matches_port(self._interface({"other": 42}), 42, "default") is True
        assert (
            interface_name_fallback_matches_port(
                self._interface({"default": {"no_id": 42}}),
                42,
                "default",
            )
            is False
        )


@pytest.mark.django_db(transaction=True)
class TestAcquireAdvisoryTransactionLock:
    def test_requires_an_open_transaction(self):
        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with pytest.raises(RuntimeError, match="requires an open transaction"):
            acquire_advisory_transaction_lock("nblp-test:no-transaction")

    def test_takes_the_lock_on_the_named_alias(self):
        from django.db import transaction

        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with transaction.atomic(using="default"):
            acquire_advisory_transaction_lock("nblp-test:aliased", using="default")

    def test_the_named_alias_selects_the_connection(self):
        """The named alias must select its connection."""
        from django.db import transaction
        from django.utils.connection import ConnectionDoesNotExist

        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with transaction.atomic(using="default"):
            with pytest.raises(ConnectionDoesNotExist):
                acquire_advisory_transaction_lock("nblp-test:unknown-alias", using="nblp-no-such-alias")

    def test_named_alias_outside_a_transaction_still_refuses(self):
        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with pytest.raises(RuntimeError, match="requires an open transaction"):
            acquire_advisory_transaction_lock("nblp-test:aliased-no-transaction", using="default")
