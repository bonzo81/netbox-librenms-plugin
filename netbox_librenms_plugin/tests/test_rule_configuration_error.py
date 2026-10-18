"""A stored rule whose regex or replacement does not work fails closed and names the rule; it never matches nothing."""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.test_modules_view import _two_server_keys, configure_servers

pytestmark = pytest.mark.django_db


def _stored(model, **fields):
    """Store a row without clean(), the way an import, the API or old data can, and read it back."""
    (row,) = model.objects.bulk_create([model(**fields)])
    return model.objects.get(pk=row.pk)


def _module_type():
    from dcim.models import Manufacturer, ModuleType

    manufacturer = Manufacturer.objects.create(name="Rule Error Vendor", slug="rule-error-vendor")
    return ModuleType.objects.create(manufacturer=manufacturer, model="Rule Error Carrier")


def _inventory_ignore_rule():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    rule = _stored(InventoryIgnoreRule, name="Broken regex", match_type="regex", pattern="(", action="skip")
    return rule, "pattern", lambda: rule.matches_name("Slot 1")


def _module_bay_mapping(**fields):
    from netbox_librenms_plugin.models import ModuleBayMapping

    def case():
        values = {"librenms_name": r"^Slot (\d+)$", "netbox_bay_name": r"Bay \1", "is_regex": True, **fields}
        mapping = _stored(ModuleBayMapping, **values)
        (field,) = fields
        return mapping, field, lambda: mapping._compiled_pattern

    return case


def _carrier_rule(field):
    from netbox_librenms_plugin.models import CarrierAutoInstallRule

    def case():
        values = {
            "device_type_pattern": "^7750",
            "librenms_child_class": "module",
            "librenms_child_name_pattern": "^Slot [AB]$",
            "netbox_bay_name_pattern": "^CMA$",
            "carrier_module_type": _module_type(),
            field: "(",
        }
        rule = _stored(CarrierAutoInstallRule, **values)
        attribute = {
            "device_type_pattern": "_compiled_device_type_pattern",
            "librenms_child_name_pattern": "_compiled_child_name_pattern",
            "netbox_bay_name_pattern": "_compiled_bay_name_pattern",
        }[field]
        return rule, field, lambda: getattr(rule, attribute)

    return case


def _port_stack_pattern(field):
    from netbox_librenms_plugin.models import PortStackLagPattern

    def case():
        values = {"librenms_os": "rule-error-os", "lag_name_pattern": "^ae", field: "("}
        pattern = _stored(PortStackLagPattern, **values)
        loader = {
            "lag_name_pattern": PortStackLagPattern.compiled_patterns_for_os,
            "sap_name_pattern": PortStackLagPattern.compiled_sap_patterns_for_os,
            "bridge_name_pattern": PortStackLagPattern.compiled_bridge_patterns_for_os,
        }[field]
        return pattern, field, lambda: loader("rule-error-os")

    return case


def _normalization_rule(**fields):
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.utils import apply_normalization_rules

    def case():
        values = {"scope": "device_type", "match_pattern": r"^WS-(\S+)$", "replacement": r"\1", **fields}
        rule = _stored(NormalizationRule, **values)
        (field,) = fields
        return rule, field, lambda: apply_normalization_rules("WS-C9300", "device_type")

    return case


def _location_parse_pattern():
    from netbox_librenms_plugin.models import LibreNMSSettings
    from netbox_librenms_plugin.utils import parse_location_for_import

    settings = LibreNMSSettings.objects.order_by("pk").first() or LibreNMSSettings()
    settings.location_parse_pattern = "(?P<site>"
    settings.location_parse_is_regex = True
    settings.save()
    return settings, "location_parse_pattern", lambda: parse_location_for_import("Site A")


CASES = {
    "inventory ignore rule pattern": _inventory_ignore_rule,
    "module bay mapping pattern": _module_bay_mapping(librenms_name="("),
    "module bay mapping template": _module_bay_mapping(netbox_bay_name=r"Bay \2"),
    "carrier device type pattern": _carrier_rule("device_type_pattern"),
    "carrier child name pattern": _carrier_rule("librenms_child_name_pattern"),
    "carrier bay name pattern": _carrier_rule("netbox_bay_name_pattern"),
    "lag name pattern": _port_stack_pattern("lag_name_pattern"),
    "sap name pattern": _port_stack_pattern("sap_name_pattern"),
    "bridge name pattern": _port_stack_pattern("bridge_name_pattern"),
    "normalization pattern": _normalization_rule(match_pattern="("),
    "normalization replacement": _normalization_rule(replacement=r"\2"),
    "location parse pattern": _location_parse_pattern,
}


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_the_evaluation_of_a_broken_stored_rule_raises_an_error_that_names_the_rule(case):
    from netbox_librenms_plugin.models import RuleConfigurationError

    rule, field, evaluate = case()

    with pytest.raises(RuleConfigurationError) as excinfo:
        evaluate()

    error = excinfo.value
    assert (error.rule_pk, error.field, error.value, error.rule_url) == (
        rule.pk,
        field,
        getattr(rule, field),
        rule.get_absolute_url(),
    )
    assert f"(ID {rule.pk}) stores an invalid {field}: {getattr(rule, field)}." in str(error)
    expected_message = str(error)
    error.args = ("private exception diagnostics",)
    assert error.public_message == expected_message


def _break_device_type_normalization():
    """Store a device_type rule that would strip the "WS-" prefix, with a regex that does not compile."""
    from netbox_librenms_plugin.models import NormalizationRule

    return _stored(NormalizationRule, scope="device_type", match_pattern="(", replacement=r"\1")


@pytest.fixture
def server(settings, monkeypatch):
    """Serve LibreNMS from a local mock server under one configured server key."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as mock:
        configure_servers(
            settings, {"rule-error": {"librenms_url": mock.url, "api_token": "test-token", "verify_ssl": False}}
        )
        mock.server_key = "rule-error"
        yield mock


def test_a_writer_refuses_the_write_and_names_the_broken_rule(client, server):
    """The device type mapping keys on the normalized hardware, so a skipped rule would store the wrong key."""
    from dcim.models import DeviceType, Manufacturer
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
    from netbox_librenms_plugin.models import DeviceTypeMapping

    rule = _break_device_type_normalization()
    manufacturer = Manufacturer.objects.create(name="Rule Error Cisco", slug="rule-error-cisco")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model="C9300", slug="rule-error-c9300")
    device_id = 4711
    libre_device = {
        "device_id": device_id,
        "hardware": "WS-C9300",
        "sysName": "rule-error-host",
        "hostname": "rule-error-host",
        "os": "ios",
        "serial": "",
        "ip": "198.18.0.71",
    }
    cache.set(get_import_device_cache_key(device_id, server.server_key), libre_device, timeout=300)
    server.device_info_response(device_id=device_id, hostname="rule-error-host", hardware="WS-C9300", serial="")
    server.vc_inventory_callable(device_id, [], {})
    client.force_login(make_superuser("rule-error-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:add_device_type_mapping", kwargs={"device_id": device_id}),
        {"device_type_id": str(device_type.pk), "server_key": server.server_key},
        HTTP_HX_REQUEST="true",
    )

    assert list(DeviceTypeMapping.objects.values_list("librenms_hardware", flat=True)) == []
    assert (response.status_code, response["HX-Reswap"]) == (200, "none")
    assert f"(ID {rule.pk}) stores an invalid match_pattern: (." in response.content.decode()


def test_a_read_only_page_shows_a_banner_that_names_the_broken_rule(client, server):
    """The sync page matches the LibreNMS hardware through the device_type rules before it renders."""
    device = make_device("rule-error-sync-page", librenms_cf={server.server_key: {"id": 4712}})
    server.device_info_response(device_id=4712, hostname=device.name, hardware="WS-C9300")
    rule = _break_device_type_normalization()
    client.force_login(make_superuser("rule-error-reader"))

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"server_key": server.server_key},
    )

    assert response.status_code == 409
    content = response.content.decode()
    assert 'id="rule-configuration-error"' in content
    assert f'<a href="{rule.get_absolute_url()}">' in content
    assert f"(ID {rule.pk}) stores an invalid <code>match_pattern</code>" in content


def test_the_chassis_fallback_of_the_device_type_match_does_not_swallow_a_broken_rule(server):
    """A hardware placeholder skips the rules, so only the chassis fallback evaluates them."""
    from netbox_librenms_plugin.import_utils.device_operations import match_import_device_type
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.models import RuleConfigurationError

    rule = _break_device_type_normalization()
    chassis = {"entPhysicalIndex": 1, "entPhysicalClass": "chassis", "entPhysicalName": "WS-C9300"}
    server.register("/api/v0/inventory/4713", {"status": "ok", "inventory": [chassis]})

    with pytest.raises(RuleConfigurationError) as excinfo:
        match_import_device_type({"device_id": 4713, "hardware": "-"}, api=LibreNMSAPI(server_key=server.server_key))

    assert (excinfo.value.rule_pk, excinfo.value.field) == (rule.pk, "match_pattern")


@pytest.fixture
def server_key(settings, librenms_server):
    return _two_server_keys(settings, librenms_server, "rule-error-modules")[0]


def test_the_modules_tab_names_a_broken_bay_mapping_and_shows_no_rows(librenms_server, server_key):
    """A regex bay mapping is read for every row that no exact mapping places, so a skip would misplace rows."""
    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.test_inventory_class_admission import _mapped_device, _recording, _refresh

    recording = _recording("junos-mx-scb")
    librenms_server.load_recording(recording)
    device = _mapped_device("mx480-broken-bay-mapping", recording, server_key)
    mapping = _stored(ModuleBayMapping, librenms_name="(", librenms_class="", netbox_bay_name="Slot 1", is_regex=True)

    response, module_sync = _refresh(device, server_key)

    assert module_sync["table"] is None
    error = module_sync["inventory_rule_error"]
    assert (error.rule_pk, error.field) == (mapping.pk, "librenms_name")
    content = response.content.decode()
    assert 'id="module-inventory-rule-error"' in content
    assert f'<a href="{mapping.get_absolute_url()}">' in content
